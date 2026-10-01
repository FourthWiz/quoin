"""Behavior, CLI and Python 3.8 floor tests for the plan pending-task helper."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "plan_tasks.py"
WRAPPER_PATH = REPO_ROOT / "quoin" / "scripts" / "plan_tasks.py"
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"
FIXTURES = Path(__file__).parent / "fixtures" / "plan_tasks"


def _load():
    spec = importlib.util.spec_from_file_location("plan_tasks_under_test", CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pt = _load()


def _plan(body: str) -> str:
    return "---\ntask: x\n---\n## For human\n\nsummary\n\n## Tasks\n\n" + body + "\n## Decisions\n\nT-99 mentioned here\n"


def _kind(body: str):
    return pt.scan_plan_text(_plan(body))


def test_all_done_check_emoji():
    s = _kind("1. ✅ T-01: a\n2. ✅ T-02: b\n")
    assert (s.kind, s.total) == ("ALLDONE", 2)


def test_all_done_check_mark():
    assert _kind("1. ✓ T-01: a\n").kind == "ALLDONE"


def test_mixed_done_glyphs():
    assert _kind("1. ✓ T-01: a\n2. ✅ T-02: b\n").kind == "ALLDONE"


@pytest.mark.parametrize("glyph", ["⏳", "\U0001F6AB", "✗", "❌", "?"])
def test_other_glyph_is_pending(glyph):
    s = _kind("1. ✅ T-01: a\n2. {} T-02: b\n".format(glyph))
    assert s.kind == "PENDING" and s.pending_ids == ["T-02"]


def test_glyphless_line_is_pending():
    s = _kind("1. ✅ T-01: a\n2. T-02: b\n")
    assert s.kind == "PENDING" and s.pending_ids == ["T-02"]


def test_heading_forms():
    assert _kind("### ✅ T-01 a\n### ✅ T-02 b\n").kind == "ALLDONE"
    s = _kind("### ✅ T-01 a\n### T-02 b\n")
    assert s.kind == "PENDING" and s.pending_ids == ["T-02"]


def test_checkbox_without_glyph_is_pending():
    assert _kind("- [x] T-01 done\n").kind == "PENDING"


def test_postfix_glyph_forms():
    assert _kind("2. [x] T-02 ✅ completed\n").kind == "ALLDONE"
    assert _kind("2. T-02 ⏳\n").kind == "PENDING"


def test_disagreeing_prefix_and_postfix_is_pending():
    assert _kind("1. ✅ T-01 ⏳ note\n").kind == "PENDING"


def test_bold_and_backtick_wrappers():
    assert _kind("1. ✅ **T-01**: a\n2. ✅ `T-02` b\n").kind == "ALLDONE"


def test_unindented_prose_lines_are_ignored():
    s = _kind("1. ✅ T-01: a\n\nT-01 → T-02 → T-03\nT-04 independent\n")
    assert s.kind == "ALLDONE" and s.total == 1


def test_indented_sub_bullet_is_ignored():
    assert _kind("1. ✅ T-01: a\n   - depends on T-02 ⏳\n").kind == "ALLDONE"


def test_fenced_block_is_ignored():
    assert _kind("1. ✅ T-01: a\n```\n2. ⏳ T-02: b\n```\n").kind == "ALLDONE"


def test_dependencies_line_is_not_a_task():
    assert _kind("1. ✅ T-01: a\n- Dependencies: T-01\n").kind == "ALLDONE"


def test_later_sections_do_not_count():
    s = pt.scan_plan_text(_plan("1. ✅ T-01: a\n"))
    assert s.total == 1


def test_duplicate_ids_need_every_line_done():
    s = _kind("1. ✅ T-01: a\n2. ⏳ T-01: again\n")
    assert s.kind == "PENDING" and s.pending_ids == ["T-01"]


def test_no_tasks_section_and_empty_and_zero_lines():
    assert pt.scan_plan_text("## Other\n\n1. ✅ T-01\n").kind == "UNKNOWN"
    assert pt.scan_plan_text("").kind == "UNKNOWN"
    assert pt.scan_plan_text("## Tasks\n\nnothing here\n").kind == "UNKNOWN"


def test_fast_route_stub_all_pending():
    s = _kind("1. ⏳ T-01: a\n2. ⏳ T-02: b\n3. ⏳ T-03: c\n")
    assert s.kind == "PENDING" and s.pending_ids == ["T-01", "T-02", "T-03"]


def test_finished_nine_task_plan_fixture():
    s = pt.scan_plan_text((FIXTURES / "finished_nine_task_plan.md").read_text(encoding="utf-8"))
    assert (s.kind, s.total) == ("ALLDONE", 9)


def _run(path_to_script: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(path_to_script), *args], capture_output=True, text=True, timeout=60
    )


@pytest.mark.parametrize("script", [CORE_PATH, WRAPPER_PATH])
def test_cli_outputs_and_exit_codes(script, tmp_path):
    done = tmp_path / "done.md"
    done.write_text(_plan("1. ✅ T-01: a\n"), encoding="utf-8")
    r = _run(script, "status", "--plan", str(done))
    assert (r.stdout.strip(), r.returncode) == ("ALLDONE|1", 0)
    pend = tmp_path / "pend.md"
    pend.write_text(_plan("1. ✅ T-01: a\n2. ⏳ T-02: b\n"), encoding="utf-8")
    r = _run(script, "status", "--plan", str(pend))
    assert (r.stdout.strip(), r.returncode) == ("PENDING|1|T-02", 1)
    r = _run(script, "status", "--plan", str(tmp_path / "missing.md"))
    assert r.stdout.startswith("UNKNOWN|") and r.returncode == 2
    r = _run(script, "bogus")
    assert (r.stdout.strip(), r.returncode) == ("UNKNOWN|usage", 2)


def test_output_is_one_sanitized_line(tmp_path):
    many = "".join("{}. ⏳ T-{}: x\n".format(i, i) for i in range(1, 300))
    p = tmp_path / "big.md"
    p.write_text(_plan(many), encoding="utf-8")
    r = _run(CORE_PATH, "status", "--plan", str(p))
    assert r.stdout.count("\n") == 1 and len(r.stdout.encode()) <= 401


def test_installer_registers_in_both_script_tuples():
    spec = importlib.util.spec_from_file_location("installer_under_test", INSTALLER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "plan_tasks.py" in mod.DEPLOYED_SCRIPTS
    assert "plan_tasks.py" in mod.CORE_SCRIPTS


def test_core_imports_under_python_38():
    exe = shutil.which("python3.8")
    if not exe:
        pytest.skip("no python3.8 on PATH")
    code = (
        "import importlib.util,sys;"
        "s=importlib.util.spec_from_file_location('m',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "print(m.scan_plan_text('## Tasks\\n1. \\u2705 T-01\\n').kind)"
    )
    r = subprocess.run([exe, "-c", code, str(CORE_PATH)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ALLDONE"


@pytest.mark.parametrize(
    "line",
    [
        "1. ✅ T-01 — x",
        "1. ✅ T-01 – x",
        "1. ✅ T-01 (x)",
        "1. ✅ **T-01** — x",
        "- ✅ T-01 → x",
        "1. ✅ T-01. x",
    ],
)
def test_punctuation_after_id_is_not_a_postfix_glyph(line):
    s = _kind(line + "\n")
    assert s.kind == "ALLDONE", line


def test_punctuation_does_not_make_a_pending_line_done():
    assert _kind("1. ⏳ T-01 — x\n").kind == "PENDING"
    assert _kind("1. T-01 — x\n").kind == "PENDING"


def test_status_line_without_id_is_unknown():
    assert _kind("1. ✅ T-01: a\n2. ⏳ polish the docs\n").kind == "UNKNOWN"


def test_unterminated_fence_is_unknown():
    assert pt.scan_plan_text("## Tasks\n\n1. ✅ T-01: a\n```\n2. ⏳ T-02: b\n").kind == "UNKNOWN"


def test_second_tasks_heading_is_unknown():
    text = "## Tasks\n\n1. ✅ T-01: a\n\n## Notes\n\nx\n\n## Tasks\n\n1. ⏳ T-02: b\n"
    assert pt.scan_plan_text(text).kind == "UNKNOWN"
