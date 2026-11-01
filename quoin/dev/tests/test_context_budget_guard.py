"""T-10 (IVG-141): context_budget_guard.py helper contract.

Import-free / bare-checkout runnable — subprocess-invokes the WRAPPER script
(quoin/scripts/context_budget_guard.py). NEVER `import quoin` (lessons 2026-07-22).

Covers: OVER on a small synthetic transcript, OK on a large budget, fail-OPEN on
missing transcript, opt-out (no read), threshold-boundary equality, env/CLI
threshold precedence, exit-code contract, formula parity vs the awk in
the byte-estimate awk in _lib.sh (the fallback used when a transcript holds no
usage record; these fixtures are all raw bytes, so they exercise that path),
bad-args fail-OPEN, --current-uuid named-jsonl resolution,
and Drive-conflict-copy filtering.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPER = REPO_ROOT / "quoin" / "scripts" / "context_budget_guard.py"
LIB_SH = REPO_ROOT / "quoin" / "hooks" / "_lib.sh"


def _run(args, env=None, **kwargs):
    return subprocess.run(
        [sys.executable, str(WRAPPER), *args],
        capture_output=True, text=True, env=env, **kwargs,
    )


def _parse(stdout: str):
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    parts = line.split("|")
    # status | util | path  (path may be empty)
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def _make_transcript(tmp_path: Path, num_bytes: int, name: str = "t.jsonl") -> Path:
    p = tmp_path / name
    p.write_bytes(b"x" * num_bytes)
    return p


def test_over_small_transcript(tmp_path):
    t = _make_transcript(tmp_path, 200_000)  # ~1666bp at 8.0/150000
    r = _run(["--transcript", str(t), "--threshold-bps", "1000"])
    status, util, path = _parse(r.stdout)
    assert status == "OVER", r.stdout
    assert int(util) >= 1000
    assert r.returncode == 1


def test_ok_large_budget(tmp_path):
    t = _make_transcript(tmp_path, 200_000)
    r = _run(["--transcript", str(t), "--threshold-bps", "9999"])
    status, util, _ = _parse(r.stdout)
    assert status == "OK"
    assert r.returncode == 0


def test_ok_empty_or_missing_transcript_failopen(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"
    r = _run(["--transcript", str(missing), "--threshold-bps", "1"])
    status, util, path = _parse(r.stdout)
    assert status == "OK"
    assert util == "0"
    assert path == ""
    assert r.returncode == 0


def test_disabled_env_no_read(tmp_path):
    t = _make_transcript(tmp_path, 200_000)
    env = {"QUOIN_DISABLE_PHASE_BUDGET": "1", "PATH": "/usr/bin:/bin"}
    r = _run(["--transcript", str(t), "--threshold-bps", "1"], env=env)
    status, util, _ = _parse(r.stdout)
    assert status == "OK"
    assert util == "disabled"
    assert r.returncode == 0


def test_threshold_boundary_equal_is_over(tmp_path):
    # 120000 bytes / 8.0 / 150000 * 10000 == exactly 1000 bp.
    t = _make_transcript(tmp_path, 120_000)
    r = _run(["--transcript", str(t), "--threshold-bps", "1000"])
    status, util, _ = _parse(r.stdout)
    assert util == "1000"
    assert status == "OVER", "util == threshold must be OVER"
    assert r.returncode == 1


def test_env_threshold_override(tmp_path):
    t = _make_transcript(tmp_path, 120_000)  # 1000 bp
    env = {"QUOIN_PHASE_BOUNDARY_BPS": "500", "PATH": "/usr/bin:/bin"}
    r = _run(["--transcript", str(t)], env=env)
    status, _, _ = _parse(r.stdout)
    assert status == "OVER"
    assert r.returncode == 1


def test_cli_threshold_beats_env(tmp_path):
    t = _make_transcript(tmp_path, 120_000)  # 1000 bp
    env = {"QUOIN_PHASE_BOUNDARY_BPS": "500", "PATH": "/usr/bin:/bin"}
    # CLI 5000 > util 1000 → OK, overriding the low env threshold.
    r = _run(["--transcript", str(t), "--threshold-bps", "5000"], env=env)
    status, _, _ = _parse(r.stdout)
    assert status == "OK"
    assert r.returncode == 0


def test_exit_codes_contract(tmp_path):
    t = _make_transcript(tmp_path, 200_000)
    assert _run(["--transcript", str(t), "--threshold-bps", "1"]).returncode == 1
    assert _run(["--transcript", str(t), "--threshold-bps", "999999"]).returncode == 0
    assert _run(["--transcript", str(tmp_path / "x.jsonl")]).returncode == 0


def test_formula_parity_fixed_byte_size(tmp_path):
    # Byte-only files carry no usage record, so the reader uses the byte estimate.
    for nbytes in (0, 8, 150_000, 1_200_000, 5_000_000):
        t = _make_transcript(tmp_path, nbytes, name=f"t{nbytes}.jsonl")
        r = _run(["--transcript", str(t), "--threshold-bps", "999999"])
        _, util, _ = _parse(r.stdout)
        expected = int((nbytes / 8.0 / 150000) * 10000)
        assert int(util) == expected, f"{nbytes} bytes: got {util}, expected {expected}"


def test_formula_parity_vs_lib_sh_awk(tmp_path):
    """Assert the Python estimate equals the awk formula literally present in
    _lib.sh (the byte-estimate fallback)."""
    import re
    text = LIB_SH.read_text(encoding="utf-8")
    # The load-bearing awk line: printf "%d\n", (b / bpt / lim) * 10000
    assert re.search(r'printf\s+"%d\\n",\s*\(b\s*/\s*bpt\s*/\s*lim\)\s*\*\s*10000',
                     text), "compute_utilization awk formula not found in _lib.sh"
    nbytes = 1_234_567
    t = _make_transcript(tmp_path, nbytes)
    r = _run(["--transcript", str(t), "--threshold-bps", "999999"])
    _, util, _ = _parse(r.stdout)
    assert int(util) == int((nbytes / 8.0 / 150000) * 10000)


def test_bad_args_failopen():
    r = _run(["--nonexistent-flag", "xyz"])
    status, util, _ = _parse(r.stdout)
    assert status == "OK"
    assert util == "0"
    assert r.returncode == 0


def test_current_uuid_resolves_named_jsonl(tmp_path, monkeypatch):
    """--current-uuid resolves <hash>/<uuid>.jsonl under a fake HOME projects dir."""
    import re as _re
    project_root = tmp_path / "proj"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    proj_hash = _re.sub(r"[^A-Za-z0-9-]", "-", str(project_root).rstrip("/"))
    proj_dir = fake_home / ".claude" / "projects" / proj_hash
    proj_dir.mkdir(parents=True)
    (proj_dir / "MYUUID.jsonl").write_bytes(b"x" * 200_000)

    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    r = _run(["--project-root", str(project_root), "--current-uuid", "MYUUID",
              "--threshold-bps", "1"], env=env)
    status, _, path = _parse(r.stdout)
    assert status == "OVER"
    assert path.endswith("MYUUID.jsonl")
    assert r.returncode == 1


def test_drive_conflict_copy_filtered(tmp_path):
    """A newer Drive conflict copy 'UUID 2.jsonl' must NOT be picked as newest."""
    import re as _re
    project_root = tmp_path / "proj"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    proj_hash = _re.sub(r"[^A-Za-z0-9-]", "-", str(project_root).rstrip("/"))
    proj_dir = fake_home / ".claude" / "projects" / proj_hash
    proj_dir.mkdir(parents=True)
    real = proj_dir / "REAL.jsonl"
    real.write_bytes(b"x" * 10_000)  # small → OK
    conflict = proj_dir / "REAL 2.jsonl"
    conflict.write_bytes(b"x" * 5_000_000)  # huge → would be OVER if picked
    # Make the conflict copy strictly newer.
    import os
    os.utime(real, (1_000_000, 1_000_000))
    os.utime(conflict, (2_000_000, 2_000_000))

    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    r = _run(["--project-root", str(project_root), "--threshold-bps", "1000"], env=env)
    status, _, path = _parse(r.stdout)
    # Should measure REAL.jsonl (small, OK), not the huge conflict copy.
    assert path.endswith("REAL.jsonl"), f"picked {path}"
    assert status == "OK"
    assert r.returncode == 0
