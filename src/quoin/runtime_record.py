"""Writes the install record that auto-resume hand-offs use to relaunch the
exact interpreter and package tree an install came from, instead of guessing
via `PATH`.

The record is a small JSON file next to a deployed `.claude/` tree. A
detached hand-off (see `quoin.core.scripts.auto_resume`) reads it to build
an argv that survives venvs, `pipx`, `uv tool`, and editable installs
without needing `quoin` on `PATH`.

Writing the record never fails the install it's attached to: every error
path here prints a warning and returns ``None``.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import quoin

RUNTIME_RECORD_FILENAME = "quoin-runtime.json"
RUNTIME_RECORD_SCHEMA = 1

_VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")


def _unlink_record_if_present(dest_root: Path) -> bool:
    """Best-effort unlink of the install record at `dest_root`. Returns
    whether a record was actually there to remove — shared by the two
    callers below, which differ only in how they report that fact."""
    path = dest_root / RUNTIME_RECORD_FILENAME
    try:
        existed = path.exists()
    except OSError:
        existed = False
    try:
        path.unlink()
    except OSError:
        pass
    return existed


def _remove_stale_record(dest_root: Path, note: str) -> str:
    """Best-effort removal of an existing record so a skipped or failed
    write never leaves an older install's record behind for a newly
    deployed hook to read. Returns `note`, extended when a record existed."""
    if _unlink_record_if_present(dest_root):
        return note + " (previous install record removed)"
    return note


def remove_existing_record(dest_root: Path) -> None:
    """Best-effort: unlinks any existing install record before the first
    deploy step of an install runs. `write_runtime_record`'s own cleanup
    (`_remove_stale_record`) only fires on its own skip/failure paths,
    which never run if the install aborts before reaching the writer at
    all (the writer is the very last step) — without this, a failed or
    partial install (missing prerequisites, a placeholder violation, a
    Ctrl-C) leaves the previous install's record in place next to a
    partially deployed hook tree, so a hand-off can relaunch an interpreter
    the new deploy never actually confirmed."""
    path = dest_root / RUNTIME_RECORD_FILENAME
    if _unlink_record_if_present(dest_root):
        print(f"quoin: removed the previous install record at {path} before deploying", file=sys.stderr)


def _unaided_quoin_file(python: str, timeout: float = 10.0) -> Optional[str]:
    """Runs `python -c "import quoin; ..."` with PYTHONPATH stripped, so the
    result reflects only what that interpreter can import on its own. None
    on any failure (non-zero exit, timeout, or a spawn error)."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    try:
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [python, "-c", "import quoin,sys;sys.stdout.write(quoin.__file__)"],
                env=env,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _detect_pythonpath(python: str) -> Optional[str]:
    unaided = _unaided_quoin_file(python)
    own_dir = Path(quoin.__file__).resolve().parent
    if unaided is not None:
        try:
            if Path(unaided).resolve().parent == own_dir:
                return None
        except OSError:
            pass
    return str(own_dir.parents[0])


def _detect_source_version(source_dir: Path) -> Optional[str]:
    about_path = source_dir / ".." / "src" / "quoin" / "__about__.py"
    try:
        if not about_path.exists():
            return None
        text = about_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def write_runtime_record(dest_root: Path, source_dir: Path) -> Optional[Path]:
    """Writes `<dest_root>/quoin-runtime.json`. Returns the written path, or
    None if the record could not be written (a warning is printed either
    way — this never raises and never changes the caller's exit code)."""
    try:
        python = sys.executable or ""
        if not python or not os.path.isabs(python):
            note = _remove_stale_record(
                dest_root,
                "quoin: could not write install record: sys.executable is empty or "
                "relative — auto-resume hand-offs will fall back to finding 'quoin' "
                "on PATH; run 'quoin doctor' to confirm.",
            )
            print(note, file=sys.stderr)
            return None

        pythonpath = _detect_pythonpath(python)
        source_version = _detect_source_version(source_dir)
        if source_version is not None and source_version != quoin.__version__:
            print(
                f"quoin: WARNING — source tree version ({source_version}) does not "
                f"match the running quoin CLI version ({quoin.__version__}); "
                "auto-resume hand-offs will be refused until quoin is installed "
                "from a matching source.",
                file=sys.stderr,
            )

        payload = {
            "schema": RUNTIME_RECORD_SCHEMA,
            "python": python,
            "version": quoin.__version__,
            "pythonpath": pythonpath,
            "quoin_file": str(Path(quoin.__file__).resolve()),
            "source_dir": str(source_dir),
            "source_version": source_version,
            "installed_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        dest_root.mkdir(parents=True, exist_ok=True)
        record_path = dest_root / RUNTIME_RECORD_FILENAME
        import json

        fd, tmp_name = tempfile.mkstemp(dir=str(dest_root), prefix=".quoin-runtime.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True)
            os.replace(str(tmp_path), str(record_path))
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
        return record_path
    except Exception as exc:  # noqa: BLE001 — a writer failure must never fail the install
        note = _remove_stale_record(dest_root, f"quoin: could not write install record ({exc})")
        print(note, file=sys.stderr)
        return None
