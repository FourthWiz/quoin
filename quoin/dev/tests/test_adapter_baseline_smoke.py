"""Smoke test for the dev-only adapter baseline instrument."""
import ast
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "adapter_baseline.py"
LABELS = {"always-on", "per-session", "per-prompt", "on-demand"}


def _home_claude(tmp_path):
    home = tmp_path / "dotclaude"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text(
        '---\nname: demo\ndescription: "A demo skill"\n---\n# Demo\n', encoding="utf-8")
    (home / "memory").mkdir()
    (home / "memory" / "m.md").write_text("memory\n", encoding="utf-8")
    (home / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
    (home / "hooks").mkdir()
    settings = {"hooks": {"UserPromptSubmit": [{"matcher": "*", "hooks": [
        {"type": "command", "command": str(home / "hooks" / "userpromptsubmit.sh")}]}]},
        "skillOverrides": {"demo": "name-only"}}
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return home


def test_script_parses_as_python_38_grammar():
    ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 8))


def _load_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_baseline_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_marker_footprint_counts_block_and_pre_marker(tmp_path):
    mod = _load_script()
    f = tmp_path / "CLAUDE.md"
    block = "# === DEV WORKFLOW START ===\nabc \u00e9\n# === DEV WORKFLOW END ==="
    f.write_text("mine\n" + block + "\ntail\n", encoding="utf-8")
    mb, pre = mod.marker_footprint(f)
    assert pre == len("mine\n")
    assert mb == {"chars": len(block), "bytes": len(block.encode("utf-8"))}


def test_marker_footprint_without_markers_is_null(tmp_path):
    mod = _load_script()
    f = tmp_path / "CLAUDE.md"
    f.write_text("just my rules\n", encoding="utf-8")
    mb, pre = mod.marker_footprint(f)
    assert mb is None
    assert pre == len("just my rules\n")
    assert mod.marker_footprint(tmp_path / "missing.md") == (None, None)


