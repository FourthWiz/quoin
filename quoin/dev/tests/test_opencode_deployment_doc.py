"""Honesty checks for the OpenCode adapter's managed-deployment claims."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

DECISIONS = Path(__file__).resolve().parents[2] / "adapters" / "opencode" / "decisions.md"
SECTION = "## Workflow parity residuals"
CLOSED_ONLY_WHEN = (
    "A write through an absolute path",
    "There is no network isolation for snapshot or test runs",
    "A passing test result attests only to what the code",
)


def _section_bullets(text: str, heading: str) -> List[str]:
    """Bullets of one section: a bullet starts at `- ` and joins every
    continuation line up to the next bullet or heading."""
    lines = text.splitlines()
    start = lines.index(heading) + 1
    bullets: List[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif line.strip() and bullets:
            bullets[-1] += " " + line.strip()
    return bullets


def test_the_parity_residual_bullets_state_when_each_limit_is_closed():
    bullets = _section_bullets(DECISIONS.read_text(encoding="utf-8"), SECTION)
    assert bullets
    for prefix in CLOSED_ONLY_WHEN:
        matching = [b for b in bullets if b.startswith(prefix)]
        assert len(matching) == 1, prefix
        assert "only when" in matching[0] and "otherwise" in matching[0], prefix
    for bullet in bullets:
        if re.search(r"\bclosed\b", bullet):
            assert "only when" in bullet, bullet


# ----------------------------------------------- managed work deployment doc

DEPLOYMENT = DECISIONS.parent / "managed-work-deployment.md"
SECTION_ORDER = (
    "Scope and status",
    "Environment duties",
    "What Quoin verifies",
    "Enforcement layers",
    "Maintainer actions",
)
DUTY_SUBSECTIONS = (
    "Filesystem mounts",
    "Secrets",
    "Process environment",
    "Egress",
    "Attestation",
    "Read-only Quoin profile files",
    "Employer-managed OpenCode settings",
)
_HOST_ALLOWED = ("example.invalid", "opencode.ai", "github.com")


def _doc() -> str:
    return DEPLOYMENT.read_text(encoding="utf-8")


def _body(text: str, level: int, heading: str) -> str:
    marker = "#" * level + " "
    lines = text.splitlines()
    start = lines.index(marker + heading) + 1
    out: List[str] = []
    for line in lines[start:]:
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            break
        out.append(line)
    return "\n".join(out)


def _bullets(body: str) -> List[str]:
    bullets: List[str] = []
    for line in body.splitlines():
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif line.strip() and bullets and line.startswith(" "):
            bullets[-1] += " " + line.strip()
    return bullets


def test_the_deployment_document_sections_are_in_order():
    text = _doc()
    h2 = [m.group(1) for m in re.finditer(r"^## (.+)$", text, re.MULTILINE)]
    assert h2 == list(SECTION_ORDER)
    h3 = [m.group(1) for m in re.finditer(r"^### (.+)$", text, re.MULTILINE)]
    assert h3 == list(DUTY_SUBSECTIONS)


def test_both_maintainer_checklists_have_items():
    body = _body(_doc(), 2, "Maintainer actions")
    parts = body.split("Live corporate capture")
    assert len(parts) == 2
    for part in parts:
        assert len(re.findall(r"^- \[ \] ", part, re.MULTILINE)) >= 3


def test_checklist_refusal_items_name_the_managed_isolation_condition():
    items = _bullets(_body(_doc(), 2, "Maintainer actions"))
    refusals = [i for i in items if re.search(r"\brefuse[sd]?\b", i)]
    assert len(refusals) >= 2
    for item in refusals:
        assert "`isolation: managed`" in item, item


def test_named_codes_exist_in_the_registries():
    import sys

    src = Path(__file__).resolve().parents[2].parent / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from quoin.opencode_adapter import doctor, launch_codes

    section = _body(_doc(), 2, "What Quoin verifies")
    tokens = set(re.findall(r"`([a-z]+(?:-[a-z0-9]+)+)`", section))
    assert tokens
    known = set(launch_codes.REGISTRY) | set(doctor.MESSAGES)
    for token in sorted(tokens):
        assert token in known, token


def test_no_line_claims_support_or_verified_isolation_without_a_limit():
    for line in _doc().splitlines():
        for match in re.finditer(r"work profile[^.\n]*\bsupported\b", line, re.IGNORECASE):
            assert re.search(r"\bnot\b", match.group(0), re.IGNORECASE), line
        for match in re.finditer(r"isolation[^.\n]*\bverified\b", line, re.IGNORECASE):
            assert re.search(r"\b(not|only when|only by|unverified)\b", line, re.IGNORECASE) or "`" in line, line


def test_the_scope_section_names_both_isolation_modes():
    scope = _body(_doc(), 2, "Scope and status")
    assert "isolation: managed" in scope and "`convenience`" in scope


def test_the_egress_duties_state_the_loopback_rule():
    bullets = _bullets(_body(_doc(), 3, "Egress"))
    assert any("loopback" in b and "approved" in b for b in bullets)


def test_the_deployment_document_names_no_unlisted_hosts():
    text = _doc()
    for host in re.findall(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|ai|dev|invalid|internal|corp|local|cloud)\b", text):
        assert any(host == a or host.endswith("." + a) for a in _HOST_ALLOWED), host
