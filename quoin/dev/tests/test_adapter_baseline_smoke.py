"""Smoke test for the dev-only adapter baseline instrument."""
import ast
import json
import subprocess
import sys
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


def test_worktree_run_emits_labelled_json(tmp_path):
    home = _home_claude(tmp_path)
    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    before = {p for p in home.rglob("*")}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--worktree", "--runs", "3", "--warmup", "1",
         "--home-claude", str(home), "--out-json", str(out_json), "--out-md", str(out_md)],
        capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    assert {p for p in home.rglob("*")} == before
    data = json.loads(out_json.read_text(encoding="utf-8"))
    for key in ("meta", "hooks", "sessionstart_outputs", "footprints", "skill_listing", "settings_hooks"):
        assert key in data
    for item in list(data["hooks"].values()) + list(data["sessionstart_outputs"].values()):
        assert item["label"] in LABELS
    assert data["footprints"]["claude_md"]["label"] in LABELS
    ss = data["skill_sources"]
    assert ss["label"] == "on-demand" and ss["label"] in LABELS
    assert ss["files"] == 32 and len(ss["skills"]) == 32
    assert data["skill_listing"]["label"] in LABELS
    assert data["skill_listing"]["name_only_skills"] == 1
    assert data["settings_hooks"][0]["per_prompt"] is True
    md = out_md.read_text(encoding="utf-8")
    assert md.startswith("## before")
    assert "### SKILL.md sources (on-demand)" in md


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


def test_generated_span_split_on_synthetic_skill(tmp_path):
    mod = _load_script()
    skill = tmp_path / "pkg" / "quoin" / "adapters" / "claude" / "skills" / "demo"
    skill.mkdir(parents=True)
    body = (
        "---\nname: demo\n---\n"                       # 3 lines
        "# Demo\n"                                       # hand
        "## \u00a70 Model dispatch\n"                   # s0 start
        "body\n"
        "<!-- \u00a70-end -->\n"                        # s0 end (3 lines)
        "\n"                                             # overhang, hand-written
        "## \u00a70\u2034 Guard\n"                     # prime: to next H2 (2 lines)
        "guard\n"
        "## \u00a70a Scope cap\n"                       # hand-written
        "prose\n"
        "## \u00a7V Reconcile\n"                        # verification (4 lines)
        "<!-- \u00a7V-reconcile-begin -->\n"
        "check\n"
        "<!-- \u00a7V-reconcile-end -->\n"
        "## Tail\n"
        "tail\n"
    )
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    r = mod.measure_skill_sources(tmp_path / "pkg")["skills"]["demo"]
    assert r["section0_lines"] == 3 + 2
    assert r["verification_lines"] == 4
    assert r["generated_lines"] == 9
    lines = body.splitlines(keepends=True)
    gen = sum(len(l.encode("utf-8")) for l in lines[4:7] + lines[8:10] + lines[12:16])
    assert r["generated_bytes"] == gen
    assert r["handwritten_bytes"] == len(body.encode("utf-8")) - gen
    assert r["total_lines"] == len(lines)


def test_implement_section0_lines_match_provenance_guard():
    mod = _load_script()
    guard_path = Path(__file__).resolve().parent / "test_section0_provenance_guard.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("_guard_under_test", guard_path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    skill = Path(__file__).resolve().parents[2] / "adapters" / "claude" / "skills" / "implement" / "SKILL.md"
    lines = skill.read_text(encoding="utf-8").splitlines()
    s0, _ver = mod.generated_span_lines(lines)
    expected = 0
    for name, variant, start, end in guard._CENSUS_BLOCKS:
        if name != "implement" or (name, variant) in guard.DECLARED_HANDWRITTEN_BLOCKS:
            continue
        stop = end
        if variant == "\u00a70":
            marker = next((i for i in range(start, end) if guard.SECTION0_END_MARKER in lines[i]), None)
            stop = (marker + 1) if marker is not None else end
        expected += stop - start
    assert len(s0) == expected
