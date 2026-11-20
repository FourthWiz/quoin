"""
Tests for the resume-cookie contract described in end_of_day/SKILL.md Step 3d
and consumed by start_of_day/SKILL.md Step 1a.

These tests use a tmp_path fixture (no real project-root writes).
"""
import os
import pathlib
import time
from datetime import datetime, timezone, timedelta

import pytest


# ---------------------------------------------------------------------------
# Minimal cookie writer/reader (mirrors what end_of_day / start_of_day do)
# ---------------------------------------------------------------------------

ALLOWLIST = {"task", "last_skill", "branch", "dirty_count", "expires"}
MAX_BYTES = 2048  # 2 KB cap


def _write_cookie(path: pathlib.Path, fields: dict, body: str) -> None:
    """Write a resume cookie using the allowlist + atomic-rename pattern."""
    extra = set(fields.keys()) - ALLOWLIST
    if extra:
        raise ValueError(f"Non-allowlisted fields: {extra}")

    frontmatter_lines = ["---"]
    for key in ("task", "last_skill", "branch", "dirty_count", "expires"):
        if key in fields:
            frontmatter_lines.append(f"{key}: {fields[key]}")
    frontmatter_lines.append("---")
    content = "\n".join(frontmatter_lines) + "\n" + body

    # Enforce 2 KB cap by truncating body
    while len(content.encode("utf-8")) > MAX_BYTES and "\n" in body:
        body = "\n".join(body.splitlines()[:-1])
        content = "\n".join(frontmatter_lines) + "\n" + body

    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.rename(str(tmp), str(path))


def _read_cookie(path: pathlib.Path):
    """Read and parse a resume cookie. Returns None if absent or stale."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None

    lines = text.splitlines()
    end_fm = lines.index("---", 1)
    fm_lines = lines[1:end_fm]
    fields = {}
    for line in fm_lines:
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()

    expires_str = fields.get("expires")
    if expires_str:
        try:
            expires = datetime.fromisoformat(expires_str)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if datetime.now(tz=timezone.utc) > expires:
                return None  # stale
        except ValueError:
            pass  # unparseable — treat as non-expiry

    body = "\n".join(lines[end_fm + 1:])
    return {"fields": fields, "body": body}


# ---------------------------------------------------------------------------
# Phase 1: write + read roundtrip
# ---------------------------------------------------------------------------

def test_roundtrip(tmp_path):
    """Fields and body survive a write→read cycle."""
    cookie_path = tmp_path / "resume-cookie.md"
    expires = (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()
    _write_cookie(
        cookie_path,
        {
            "task": "my-task",
            "last_skill": "end_of_day",
            "branch": "feat/my-branch",
            "dirty_count": "2",
            "expires": expires,
        },
        "Continue implementing T-03.",
    )
    result = _read_cookie(cookie_path)
    assert result is not None
    assert result["fields"]["task"] == "my-task"
    assert result["fields"]["last_skill"] == "end_of_day"
    assert result["fields"]["branch"] == "feat/my-branch"
    assert result["fields"]["dirty_count"] == "2"
    assert "Continue implementing T-03." in result["body"]


# ---------------------------------------------------------------------------
# Phase 2: stale cookie graceful fallback
# ---------------------------------------------------------------------------

def test_stale_cookie_returns_none(tmp_path):
    """A cookie with expires < now returns None (graceful fallback)."""
    cookie_path = tmp_path / "resume-cookie.md"
    past = (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat()
    _write_cookie(
        cookie_path,
        {"task": "old-task", "last_skill": "end_of_day", "branch": "main",
         "dirty_count": "0", "expires": past},
        "Old hint.",
    )
    assert _read_cookie(cookie_path) is None


# ---------------------------------------------------------------------------
# Phase 3: absent cookie graceful fallback
# ---------------------------------------------------------------------------

def test_absent_cookie_returns_none(tmp_path):
    """If no cookie exists, reader returns None (graceful fallback)."""
    cookie_path = tmp_path / "resume-cookie.md"
    assert _read_cookie(cookie_path) is None


# ---------------------------------------------------------------------------
# Phase 4: non-allowlisted field rejected
# ---------------------------------------------------------------------------

def test_allowlist_rejects_extra_field(tmp_path):
    """Writer must raise ValueError for non-allowlisted fields (R-6 mitigation)."""
    cookie_path = tmp_path / "resume-cookie.md"
    expires = (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()
    with pytest.raises(ValueError, match="Non-allowlisted"):
        _write_cookie(
            cookie_path,
            {
                "task": "my-task",
                "last_skill": "end_of_day",
                "branch": "main",
                "dirty_count": "0",
                "expires": expires,
                "secret_token": "SENSITIVE",  # not in allowlist
            },
            "hint",
        )


# ---------------------------------------------------------------------------
# Phase 5: body > 2 KB is truncated
# ---------------------------------------------------------------------------

def test_size_cap_truncates_body(tmp_path):
    """Writer truncates the body so the final file is ≤ 2 KB."""
    cookie_path = tmp_path / "resume-cookie.md"
    expires = (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()
    long_body = "\n".join([f"Line {i}: " + "x" * 80 for i in range(50)])
    _write_cookie(
        cookie_path,
        {"task": "t", "last_skill": "end_of_day", "branch": "main",
         "dirty_count": "0", "expires": expires},
        long_body,
    )
    size = cookie_path.stat().st_size
    assert size <= MAX_BYTES, f"Cookie exceeded {MAX_BYTES} bytes: {size}"


# ---------------------------------------------------------------------------
# Atomic rename: no .tmp left behind after successful write
# ---------------------------------------------------------------------------

def test_no_tmp_left_behind(tmp_path):
    """Atomic rename must not leave a .tmp file on successful write."""
    cookie_path = tmp_path / "resume-cookie.md"
    expires = (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()
    _write_cookie(
        cookie_path,
        {"task": "t", "last_skill": "end_of_day", "branch": "main",
         "dirty_count": "0", "expires": expires},
        "hint",
    )
    tmp_path_candidate = cookie_path.with_suffix(".tmp")
    assert not tmp_path_candidate.exists(), ".tmp file left behind after write"
