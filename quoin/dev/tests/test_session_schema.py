"""Session-state schema: the doc, the sidecar, every reader and every writer agree.

Each case reads the real artifact it compares against; nothing compares a file
with a copy of itself.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent  # quoin/quoin
REPO = PKG.parent
CORE_SCRIPTS = PKG / "core" / "scripts"
SKILLS = PKG / "adapters" / "claude" / "skills"
DOC = PKG / "memory" / "session-schema.md"
SIDECAR = PKG / "memory" / "format-kit.sections.json"
HOOKS = PKG / "hooks"

sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

import checkpoint_picker  # noqa: E402
import select_unprocessed_sessions as sus  # noqa: E402
import verify_claims  # noqa: E402

DOC_TEXT = DOC.read_text(encoding="utf-8")
DEMO_DAY = date(2026, 1, 2)

_writer_skills = (
    "architect", "critic", "enrich", "plan", "implement",
    "revise", "revise-fast", "review", "security_review", "specify",
)
SCHEMA_CITE = "memory/session-schema.md"
OLD_TEMPLATE_MARKERS = (
    "- **Status:**",
    "- **## Status:**",
    "- **Completed in this session:**",
    "Recorded in cost ledger",
)


def _skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _slice(text: str, start: str, end_re: str) -> str:
    """Text from the first line starting with `start` to the next line matching end_re."""
    i = text.index(start)
    m = re.search(end_re, text[i + len(start):], re.M)
    return text[i:] if m is None else text[i:i + len(start) + m.start()]


def _fence(info: str) -> str:
    m = re.search(r"```" + re.escape(info) + r"\n(.*?)```", DOC_TEXT, re.S)
    assert m, f"no fenced block with info string {info!r} in session-schema.md"
    body = m.group(1)
    assert "## Status" in body and "## Cost" in body, f"{info} slice is empty or truncated"
    return body


def _items(block: str, heading: str) -> list[str]:
    m = re.search(r"^" + re.escape(heading) + r"\n(.*?)(?=^## |\Z)", block, re.M | re.S)
    assert m, f"{heading} missing"
    return [ln for ln in m.group(1).splitlines() if ln.strip().startswith("- ")]


def _cost_value(block: str, key: str) -> str | None:
    m = re.search(r"^- " + re.escape(key) + r":\s*(.*)$", block, re.M)
    return m.group(1).strip() if m else None


def _validate(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "validate_artifact.py"),
         "--sections-json", str(SIDECAR), str(path)],
        capture_output=True, text=True,
    )


def _project(tmp_path: Path, name: str, content: str, day: str = "2026-01-02") -> Path:
    sessions = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    p = sessions / f"{day}-{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


EXAMPLE = _fence("markdown")


# ---------------------------------------------------------------- registration

def test_registered_for_install_and_catalog():
    from quoin import installer

    assert "session-schema.md" in installer.TIER1_MEMORY_FILES
    assert "session-schema.md" in (PKG / "memory" / "tier1-files.md").read_text(encoding="utf-8")


def test_doc_and_sidecar_parity():
    session = json.loads(SIDECAR.read_text(encoding="utf-8"))["artifact_types"]["session"]
    rows = {}
    for ln in DOC_TEXT.splitlines():
        m = re.match(r"\| `(## [^`]+)` \| ([^|]+) \|", ln)
        if m:
            rows[m.group(1)] = m.group(2).strip()
    assert set(rows) == set(session["allowed_sections"]) | {"## Cost"}
    for heading in session["required_sections"]:
        assert rows[heading].startswith("required"), heading
    assert "## Next command" in session["allowed_sections"]
    assert "## Next command" not in session["required_sections"]
    assert "## Next command" in rows


def test_example_validates(tmp_path):
    p = _project(tmp_path, "demo", EXAMPLE)
    r = _validate(p)
    assert r.returncode == 0, r.stdout + r.stderr


# ----------------------------------------------------- reader compatibility

def test_readers_accept_example(tmp_path):
    p = _project(tmp_path, "demo", EXAMPLE)
    selected = sus.select_unprocessed_sessions(tmp_path, DEMO_DAY, source="daily")
    assert p in selected

    before = verify_claims.check_side_effects(tmp_path, "end_of_day", today=DEMO_DAY)
    assert f"flags_flipped:{p.name}" in before["missing"]

    flipped = sus.flip_finalized_task_sessions(tmp_path, "demo", "2026-01-03")
    assert p in flipped
    text = p.read_text(encoding="utf-8")
    assert _cost_value(text, "end_of_day_due") == "no"
    assert _cost_value(text, "finalized_by_end_of_task") == "2026-01-03"
    assert text.index("finalized_by_end_of_task") > text.index("## Cost")
    assert _validate(p).returncode == 0

    assert p in sus.find_finalized_marked(tmp_path, DEMO_DAY, DEMO_DAY)
    after = verify_claims.check_side_effects(tmp_path, "end_of_day", today=DEMO_DAY)
    assert f"flags_flipped:{p.name}" not in after["missing"]

    uuid = _cost_value(EXAMPLE, "Session UUID")
    assert checkpoint_picker._session_matches_sid(EXAMPLE, uuid)


def test_fallback_fires_regex_is_pinned_in_both_skills():
    rx = r"^- fallback_fires:\s*(\d+)\s*$"
    for name in ("cost_snapshot", "end_of_day"):
        assert rx in _skill(name), name
    m = re.search(rx, EXAMPLE, re.M)
    assert m and m.group(1) == "0"


# ------------------------------------------------------------ same-day merge

def test_same_day_merge_example(tmp_path):
    before, after = _fence("text merge-before"), _fence("text merge-after")
    for heading in ("## Completed in this session", "## Decisions made"):
        b, a = _items(before, heading), _items(after, heading)
        assert all(item in a for item in b), heading
        assert len(a) > len(b), heading
        assert len(a) == len(set(a)), f"duplicate items under {heading}"
    for key in ("fallback_fires", "finalized_by_end_of_task"):
        assert _cost_value(before, key) is not None
        assert _cost_value(after, key) == _cost_value(before, key)
    assert _cost_value(after, "end_of_day_due") == "yes"
    p = _project(tmp_path, "demo", after)
    assert _validate(p).returncode == 0
    assert p in sus.select_unprocessed_sessions(tmp_path, DEMO_DAY, source="daily")


# --------------------------------------------------------------------- hooks

def _hook_world(tmp_path: Path):
    """A temp project whose session file is dated today and ~30 minutes old."""
    root = tmp_path / "proj"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    today = time.strftime("%Y-%m-%d")
    p = _project(root, "demo", EXAMPLE, day=today)
    old = time.time() - 1800
    os.utime(p, (old, old))
    env = dict(os.environ, HOME=str(home), TMPDIR=str(tmpdir))
    return root, home, p, env


needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="hooks need jq")


@needs_jq
def test_sessionstart_banner_names_example(tmp_path):
    root, _home, _p, env = _hook_world(tmp_path)
    stdin = json.dumps({"session_id": "x", "source": "startup", "cwd": str(root)})
    r = subprocess.run(["sh", str(HOOKS / "sessionstart.sh")], input=stdin,
                       capture_output=True, text=True, env=env)
    assert "[quoin-S-4]" in r.stdout and "demo" in r.stdout, r.stdout + r.stderr


@needs_jq
def test_sessionend_nudges_and_leaves_file_untouched(tmp_path):
    root, home, p, env = _hook_world(tmp_path)
    resolved = subprocess.run(
        ["sh", "-c", f'. "{HOOKS}/_lib.sh" && resolve_project_root "{root}"'],
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    proj_hash = resolved.replace("/", "-")
    jsonl_dir = home / ".claude" / "projects" / proj_hash
    jsonl_dir.mkdir(parents=True)
    jsonl = jsonl_dir / "deadbeef-0000-1234-5678-abcdef000001.jsonl"
    jsonl.write_text("{}")  # newer than the session file, so a snapshot step would fire
    before = p.read_bytes()
    stdin = json.dumps({"session_id": "x", "cwd": str(root)})
    r = subprocess.run(["sh", str(HOOKS / "sessionend.sh")], input=stdin,
                       capture_output=True, text=True, env=env)
    assert "[quoin-S-4]" in r.stdout and "demo" in r.stdout, r.stdout + r.stderr
    assert p.read_bytes() == before
    assert "## Close snapshot" not in p.read_text(encoding="utf-8")


# -------------------------------------------------------- orchestrator variant

def test_orchestrator_variant_headings_documented(tmp_path):
    sessions = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions.mkdir(parents=True)
    ss = sessions / "2026-01-02-demo-orchestrator.md"
    r = subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "thorough_plan_checkpoint.py"),
         "--project-root", str(tmp_path), "--task", "demo", "--round", "1",
         "--phase", "plan", "--session-state", str(ss)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert ss.is_file()
    section = DOC_TEXT[DOC_TEXT.index("## Orchestrator variant"):]
    headings = re.findall(r"^(## [A-Za-z ]+?)(?::|\s*$)", ss.read_text(encoding="utf-8"), re.M)
    assert headings
    for h in headings:
        assert h in section, h


# -------------------------------------------------------------- writer pins

@pytest.mark.parametrize("name", _writer_skills)
def test_save_session_state_slice_points_at_schema(name):
    sl = _slice(_skill(name), "\n## Save session state\n", r"^## ")
    assert SCHEMA_CITE in sl, name
    for marker in OLD_TEMPLATE_MARKERS:
        assert marker not in sl, f"{name}: {marker!r}"
    assert "YAML" not in sl, name


_named_steps = {
    "end_of_day": ("### Step 1: Save current session state", r"^### Step "),
    "end_of_task": ("3. Update", r"^\s+3a\. "),
    "rollback": ("### Step 5: Update session state", r"^### "),
    "run_hard": ("## Session state tracking", r"^## "),
}


@pytest.mark.parametrize("name", sorted(_named_steps))
def test_named_session_step_points_at_schema(name):
    start, end = _named_steps[name]
    text = _skill(name)
    if name == "end_of_task":
        start = "3. Update `.workflow_artifacts/memory/sessions/<date>-<task_name>.md`"
        end = r"^\s+3a\. "
    sl = _slice(text, start, end)
    assert SCHEMA_CITE in sl, name
    for marker in OLD_TEMPLATE_MARKERS:
        assert marker not in sl, f"{name}: {marker!r}"


@pytest.mark.parametrize("name", ("pr", "workspace"))
def test_pr_and_workspace_session_lines_point_at_schema(name):
    text = _skill(name)
    sl = _slice(text, "## Session state", r"^## ")
    assert SCHEMA_CITE in sl, name
    line = next(ln for ln in text.splitlines()
                if ln.lstrip().startswith("2. Write session state to"))
    idx = text.index(line)
    assert SCHEMA_CITE in text[idx:idx + 400], name


def test_file_wide_absences():
    assert "After each task (or at natural stopping points)" not in _skill("implement")
    assert "Record `autonomous: true` in the session-state file" not in _skill("run_hard")
    assert "[autonomous]" in _skill("run_hard")  # the cost-ledger NOTE tag stays
    for skill_dir in SKILLS.iterdir():
        md = skill_dir / "SKILL.md"
        if md.is_file():
            assert "pollution_score" not in md.read_text(encoding="utf-8"), skill_dir.name


# ------------------------------------------------------------------- triage

def _triage_command() -> str:
    text = _skill("triage")
    m = re.search(r"\| Active session file with status `in_progress` \| `(awk .*?2>/dev/null)` non-empty", text)
    assert m, "triage row for the session signal not found"
    return m.group(1)


def test_triage_signal_reads_both_forms(tmp_path):
    cmd = _triage_command()
    today = time.strftime("%Y-%m-%d")
    sessions = tmp_path / ".workflow_artifacts" / "memory" / "sessions"
    sessions.mkdir(parents=True)

    def run() -> str:
        return subprocess.run(["sh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True).stdout

    (sessions / f"{today}-new.md").write_text("## Status\nin_progress\n\n## Cost\n- x: y\n")
    assert f"{today}-new.md" in run()
    (sessions / f"{today}-new.md").unlink()

    (sessions / f"{today}-legacy.md").write_text("## Status: in_progress\n")
    assert f"{today}-legacy.md" in run()
    (sessions / f"{today}-legacy.md").unlink()

    (sessions / f"{today}-done.md").write_text("## Status\ncompleted\n")
    (sessions / f"{today}-demo-orchestrator.md").write_text("## Status\nin_progress\n")
    assert run().strip() == ""


# ---------------------------------------------------------------- gate, rules

def test_gate_profile_source_is_plan_frontmatter():
    text = _skill("gate")
    para = _slice(text, "## Determining the gate level", r"^## ")
    assert "profile:" in para
    assert "or from the session state file" not in para


def test_claude_md_points_at_schema():
    text = (PKG / "CLAUDE.md").read_text(encoding="utf-8")
    sec = _slice(text, "### Session state tracking", r"^### |^## ")
    assert "session-schema.md" in sec
    assert "at natural checkpoints" not in sec
