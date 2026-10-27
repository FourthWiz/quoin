"""Tests for the pure patch-size measure."""
from __future__ import annotations

import pytest

from quoin.benchmarks.harness import patch_size
from quoin.benchmarks.harness.patch_size import is_test_path, measure_patch


def _hunk_patch(path, removed, added, header=None):
    header = header or f"diff --git a/{path} b/{path}"
    body = "".join(f"-old {i}\n" for i in range(removed)) + "".join(f"+new {i}\n" for i in range(added))
    return f"{header}\nindex 111..222 100644\n--- a/{path}\n+++ b/{path}\n@@ -1,{removed} +1,{added} @@\n{body}"


def _m(text):
    return measure_patch(text.encode("utf-8"))


def test_ac7_source_and_test_split():
    text = _hunk_patch("src/app.py", 2, 5) + _hunk_patch("tests/test_app.py", 0, 12)
    r = _m(text)
    assert r["status"] == "ok"
    assert r["non_test"] == {"added": 5, "removed": 2, "files": 1}
    assert r["test"] == {"added": 12, "removed": 0, "files": 1}
    assert r["files"] == 2 and r["lines_changed"] == 19 and r["binary_files"] == 0


def test_empty_and_missing():
    assert measure_patch(b"")["status"] == "empty"
    assert measure_patch(None)["status"] == "missing"
    assert measure_patch(b"not a diff at all\n")["status"] == "empty"
    assert measure_patch(b"")["files"] == 0


def test_rename_uses_the_new_path():
    text = (
        "diff --git a/src/old.py b/tests/new_test.py\nsimilarity index 90%\n"
        "rename from src/old.py\nrename to tests/new_test.py\n"
        "--- a/src/old.py\n+++ b/tests/new_test.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    r = _m(text)
    assert r["test"]["files"] == 1 and r["non_test"]["files"] == 0


def test_pure_rename_counts_a_file_with_no_lines():
    text = (
        "diff --git a/src/old.py b/src/new.py\nsimilarity index 100%\n"
        "rename from src/old.py\nrename to src/new.py\n"
    )
    r = _m(text)
    assert r["files"] == 1 and r["lines_changed"] == 0 and r["status"] == "ok"


def test_deletion_uses_the_old_path():
    text = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\ndeleted file mode 100644\nindex 111..000\n"
        "--- a/tests/test_x.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    )
    r = _m(text)
    assert r["test"] == {"added": 0, "removed": 2, "files": 1}


def test_binary_differ_form_and_git_binary_patch_form():
    differ = (
        "diff --git a/docs/img.png b/docs/img.png\nindex 1..2 100644\n"
        "Binary files a/docs/img.png and b/docs/img.png differ\n"
    )
    r = _m(differ)
    assert r["binary_files"] == 1 and r["files"] == 1 and r["lines_changed"] == 0
    blob = (
        "diff --git a/tests/data.bin b/tests/data.bin\nnew file mode 100644\nindex 000..abc\n"
        "GIT binary patch\nliteral 12\nzc$@(abcdefghijklmn\n\nliteral 0\nKcmV+b0RR6000031\n\n"
    )
    r = _m(blob)
    assert r["binary_files"] == 1 and r["test"]["files"] == 1 and r["lines_changed"] == 0


def test_new_empty_file_under_tests():
    text = "diff --git a/tests/__init__.py b/tests/__init__.py\nnew file mode 100644\nindex 0000000..e69de29\n"
    r = _m(text)
    assert r["status"] == "ok" and r["test"] == {"added": 0, "removed": 0, "files": 1}


def test_deleted_empty_file_uses_the_old_path():
    text = "diff --git a/tests/__init__.py b/tests/__init__.py\ndeleted file mode 100644\nindex e69de29..0000000\n"
    assert _m(text)["test"]["files"] == 1


def test_mode_only_change():
    text = "diff --git a/src/run.sh b/src/run.sh\nold mode 100644\nnew mode 100755\n"
    r = _m(text)
    assert r["non_test"] == {"added": 0, "removed": 0, "files": 1} and r["lines_changed"] == 0


def test_mixed_mode_only_and_hunk_sections():
    text = "diff --git a/a.sh b/a.sh\nold mode 100644\nnew mode 100755\n" + _hunk_patch("b.py", 1, 3)
    r = _m(text)
    assert r["non_test"] == {"added": 3, "removed": 1, "files": 2}


@pytest.mark.parametrize("quoted_header", [True, False])
def test_c_quoted_path_with_a_space(quoted_header):
    header = 'diff --git "a/tests/na me.py" "b/tests/na me.py"'
    text = header + '\nindex 1..2 100644\n--- "a/tests/na me.py"\n+++ "b/tests/na me.py"\n@@ -1 +1 @@\n-a\n+b\n'
    if not quoted_header:
        text = header + "\nnew file mode 100644\nindex 1..2\n"
    r = _m(text)
    assert r["test"]["files"] == 1


def test_octal_escaped_utf8_path():
    text = 'diff --git "a/src/caf\\303\\251.py" "b/src/caf\\303\\251.py"\nnew file mode 100644\nindex 0..1\n'
    r = _m(text)
    assert r["status"] == "ok" and r["non_test"]["files"] == 1
    assert patch_size._unquote('"src/caf\\303\\251.py"') == "src/café.py"
    assert patch_size._unquote('"a\\tb\\"c\\\\d"') == 'a\tb"c\\d'


def test_unresolvable_path_makes_the_patch_unreadable():
    text = "diff --git weird\nnew file mode 100644\n"
    assert _m(text)["status"] == "unreadable"


def test_removed_line_starting_with_dashes_is_counted():
    text = (
        "diff --git a/src/x.sql b/src/x.sql\nindex 1..2 100644\n--- a/src/x.sql\n+++ b/src/x.sql\n"
        "@@ -1,2 +1,2 @@\n-- foo\n--- bar\n+++ baz\n+ok\n"
    )
    r = _m(text)
    assert r["non_test"]["added"] == 2 and r["non_test"]["removed"] == 2


def test_no_newline_marker_is_ignored():
    text = _hunk_patch("src/x.py", 1, 1) + "\\ No newline at end of file\n"
    assert _m(text)["lines_changed"] == 2


def test_crlf_patch():
    text = _hunk_patch("src/x.py", 1, 2).replace("\n", "\r\n")
    r = _m(text)
    assert r["non_test"] == {"added": 2, "removed": 1, "files": 1}


@pytest.mark.parametrize("path,expected", [
    ("conftest.py", True), ("pkg/conftest.py", True), ("docs/x.md", False),
    ("tests/a.py", True), ("pkg/test/a.py", True), ("pkg/testing/a.py", True),
    ("pkg/test_a.py", True), ("pkg/a_test.py", True), ("pkg/tests.py", True),
    ("pkg/unit_tests.py", True), ("src/contest.py", False), ("src/latest.py", False),
])
def test_test_path_classification(path, expected):
    assert is_test_path(path) is expected


def test_rule_sentence_names_the_conventions():
    assert "conftest.py" in patch_size.TEST_FILE_RULE


def test_out_of_range_octal_escape_is_unreadable():
    with pytest.raises(patch_size._Unreadable):
        patch_size._unquote('"\\777.py"')
    patch = 'diff --git "a/\\777.py" "b/\\777.py"\n--- "a/\\777.py"\n+++ "b/\\777.py"\n@@ -1 +1 @@\n-a\n+b\n'
    assert patch_size.measure_patch(patch.encode())["status"] == "unreadable"
