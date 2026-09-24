"""Static checks over the OpenCode adapter's status documents.

Pure file reads plus one `git ls-files` subprocess call; no network access
and no live OpenCode process. These tests pin document structure (headings,
status grammar, template field names) rather than prose, so they stay stable
against later wording edits while still catching a claim that silently loses
its evidence binding to the pinned release.
"""
from __future__ import annotations

import re
import subprocess

import _opencode_helpers as helpers

probe = helpers.load_module(helpers.OPENCODE_DIR / "probe_gateway.py", "quoin_opencode_probe_gateway_docs")

REPO_ROOT = helpers.OPENCODE_DIR.parent.parent.parent
COMPAT_PATH = helpers.OPENCODE_DIR / "compatibility.md"
DECISIONS_PATH = helpers.OPENCODE_DIR / "decisions.md"
README_PATH = helpers.OPENCODE_DIR / "README.md"
ADAPTERS_README_PATH = REPO_ROOT / "quoin" / "adapters" / "README.md"
STATUS_PATH = REPO_ROOT / "quoin" / "docs" / "runtime-portability-status.md"

CLAIM_HEADINGS = (
    "CLI run invocation and JSON event output",
    "Configuration sources and precedence",
    "Skills: names and discovery locations",
    "Commands, agents, delegation and permissions",
    "Custom providers",
    "Models and variants",
    "Instructions, rules and AGENTS.md",
    "Provider policy and tool permissions",
    "Plugins and events",
)

DECISION_HEADINGS = (
    "Gateway API family",
    "Gateway base URL",
    "Authentication method",
    "TLS and proxy",
    "Model IDs",
    "Rate limits",
    "Context limits",
    "OpenCode release and install channel",
    "Execution isolation",
    "Jira, Slack and mail clients",
    "Tenants",
    "Mail backend",
    "Retention",
    "Egress",
    "Interactive-first or headless-first",
)

# A public documentation host is added here only when a claim actually cites
# it, in the same commit as the citation, with a reason comment.
ALLOWLIST = (
    "127.0.0.1",
    "localhost",
    "example.invalid",
    "opencode.ai",  # the pinned upstream project's site (config schema, docs)
    "github.com",  # tag-pinned source citations
    "githubusercontent.com",
    "openrouter.ai",  # example gateway host in adjacent docs
    "npmjs.com",  # install-channel dist-tag research
    "npmjs.org",
    "models.dev",  # OpenCode's own provider catalogue, referenced upstream
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _all_headings(text):
    return [
        (len(m.group(1)), m.group(2).strip(), m.start(), m.end())
        for m in _HEADING_RE.finditer(text)
    ]


def _sections(text, level):
    """Map heading text -> body, for every heading at `level`.

    A section's body runs until the next heading whose level is <= `level`
    (a coarser or equal heading also closes it), not just the next heading
    of the exact same level.
    """
    headings = _all_headings(text)
    out = {}
    for i, (lvl, name, _start, end) in enumerate(headings):
        if lvl != level:
            continue
        content_end = len(text)
        for lvl2, _name2, start2, _end2 in headings[i + 1 :]:
            if lvl2 <= level:
                content_end = start2
                break
        out[name] = text[end:content_end]
    return out


def _shipped_files():
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "quoin/adapters/opencode"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        paths = [p for p in result.stdout.splitlines() if p]
    except Exception:
        paths = []
        for p in (REPO_ROOT / "quoin" / "adapters" / "opencode").rglob("*"):
            if p.is_dir():
                continue
            rel = p.relative_to(REPO_ROOT).as_posix()
            parts = rel.split("/")
            if any(part.startswith(".") for part in parts) or "__pycache__" in parts:
                continue
            paths.append(rel)
    return sorted(set(paths))


def _table_rows(section_text):
    rows = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        if cells[0] in ("Claim", "Key field", "Capability", "Verdict field"):
            continue
        if set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def _parse_pin(compat_text):
    sections = _sections(compat_text, 2)
    pin_block = sections["Pinned release"]
    version = re.search(r"^Version:\s*(\d+)\.(\d+)\.(\d+)", pin_block, re.MULTILINE)
    assert version, "no Version: line in ## Pinned release"
    repo_url = re.search(
        r"^Upstream repository:\s*https?://github\.com/([^/\s]+)/([^/\s]+)", pin_block, re.MULTILINE
    )
    assert repo_url, "no Upstream repository: line in ## Pinned release"
    pinned_tuple = tuple(int(x) for x in version.groups())
    return pinned_tuple, repo_url.group(1), repo_url.group(2)


def _tag_pinned_ok(evidence, owner, repo, pinned_tuple):
    pattern = re.compile(
        re.escape(owner) + r"/" + re.escape(repo) + r"/(?:blob|tree|releases/tag)/v?(\d+)\.(\d+)\.(\d+)\b"
    )
    for m in pattern.finditer(evidence):
        tup = tuple(int(x) for x in m.groups())
        if tup[0] == pinned_tuple[0] and tup <= pinned_tuple:
            return True
    return False


def _versioned_docs_ok(evidence, pinned_major):
    for m in re.finditer(r"opencode\.ai/v(\d+)/docs", evidence):
        if int(m.group(1)) == pinned_major:
            return True
    return False


def _command_span_ok(evidence, pinned_tuple):
    version_str = "%d.%d.%d" % pinned_tuple
    if version_str not in evidence:
        return False
    return re.search(r"`opencode [^`]+`", evidence) is not None


def claim_row_ok(status, evidence, pinned_tuple, owner, repo):
    if status == "verified":
        return (
            _tag_pinned_ok(evidence, owner, repo, pinned_tuple)
            or _versioned_docs_ok(evidence, pinned_tuple[0])
            or _command_span_ok(evidence, pinned_tuple)
        )
    return bool(re.match(r"^unverified\s*(—|-)\s*\S", status))


def _opencode_status_section():
    text = STATUS_PATH.read_text(encoding="utf-8")
    sections = _sections(text, 2)
    return sections.get("OpenCode", "")


def _opencode_adapters_bullet():
    text = ADAPTERS_README_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("- OpenCode:"):
            return line
    return ""


def test_documents_exist_and_are_linked():
    assert COMPAT_PATH.exists()
    assert DECISIONS_PATH.exists()
    readme = README_PATH.read_text(encoding="utf-8")
    assert "compatibility.md" in readme
    assert "decisions.md" in readme
    adapters_readme = ADAPTERS_README_PATH.read_text(encoding="utf-8")
    assert "OpenCode: qualification in progress" in adapters_readme
    assert "opencode/compatibility.md" in adapters_readme
    assert "opencode/decisions.md" in adapters_readme


def test_status_page_opencode_section():
    text = STATUS_PATH.read_text(encoding="utf-8")
    order = [m.group(1).strip() for m in re.finditer(r"^## (.+)$", text, re.MULTILINE)]
    assert "OpenCode" in order
    assert order.index("OpenCode") == order.index("Codex") + 1
    assert order.index("OpenCode") == order.index("Portable Core") - 1
    body = _opencode_status_section()
    assert "qualification in progress" in body
    assert "no runtime integration" in body
    assert "compatibility.md" in body
    assert "decisions.md" in body
    assert "installable" not in body.lower()
    assert "~/." not in body


def test_compatibility_pinned_release():
    text = COMPAT_PATH.read_text(encoding="utf-8")
    sections = _sections(text, 2)
    pin = sections["Pinned release"]
    assert re.search(r"^Version:\s*\d+\.\d+\.\d+\s*$", pin, re.MULTILINE)
    channel = re.search(r"^Install channel:\s*(\S.*)$", pin, re.MULTILINE)
    assert channel and channel.group(1).strip()
    assert re.search(r"^Verified on:\s*\d{4}-\d{2}-\d{2}\s*$", pin, re.MULTILINE)


def test_compatibility_claim_areas():
    text = COMPAT_PATH.read_text(encoding="utf-8")
    sections = _sections(text, 2)
    for heading in CLAIM_HEADINGS:
        assert heading in sections, heading


def test_compatibility_claim_rows():
    text = COMPAT_PATH.read_text(encoding="utf-8")
    pinned_tuple, owner, repo = _parse_pin(text)
    sections = _sections(text, 2)
    for heading in CLAIM_HEADINGS:
        rows = _table_rows(sections[heading])
        assert rows, "no rows under %s" % heading
        for cells in rows:
            assert len(cells) == 4, (heading, cells)
            _claim, status, evidence, _note = cells
            assert claim_row_ok(status, evidence, pinned_tuple, owner, repo), (heading, cells)


def test_compatibility_release_lines():
    text = COMPAT_PATH.read_text(encoding="utf-8")
    sections = _sections(text, 2)
    assert "Release lines" in sections
    body = sections["Release lines"]
    majors = set(re.findall(r"(\d+)\.\d+\.x", body)) | set(re.findall(r"(\d+)\.\d+\.\d+", body))
    assert len(majors) >= 2, majors
    assert "Quoin for OpenCode development specification" in body


def test_decisions_sections():
    text = DECISIONS_PATH.read_text(encoding="utf-8")
    sections = _sections(text, 3)
    for heading in DECISION_HEADINGS:
        assert heading in sections, heading
        body = sections[heading]
        assert re.search(r"^Status:\s*TODO\s*$", body, re.MULTILINE), heading
        assert re.search(r"^Owner:\s*maintainer\s*$", body, re.MULTILINE), heading
        blocks = re.search(r"^Blocks:\s*(\S.*)$", body, re.MULTILINE)
        assert blocks and blocks.group(1).strip(), heading
        assert re.search(r"^Value:\s*not set\s*$", body, re.MULTILINE), heading
        has_proposed = re.search(r"^Proposed:", body, re.MULTILINE) is not None
        if heading == "OpenCode release and install channel":
            assert has_proposed, heading
        else:
            assert not has_proposed, heading


def test_decisions_proposed_release_matches_pin():
    pinned_tuple, _owner, _repo = _parse_pin(COMPAT_PATH.read_text(encoding="utf-8"))
    pin_str = "%d.%d.%d" % pinned_tuple
    sections = _sections(DECISIONS_PATH.read_text(encoding="utf-8"), 3)
    release_body = sections["OpenCode release and install channel"]
    proposed = re.search(r"^Proposed:\s*(\d+\.\d+\.\d+)", release_body, re.MULTILINE)
    assert proposed and proposed.group(1) == pin_str


def test_probe_results_template_mirrors_record():
    report = probe.ProbeReport(context=None, steps=[], verdict="could_not_run", blocking_step=None)
    record = probe.build_capability_record(report, config=None)

    sections = _sections(DECISIONS_PATH.read_text(encoding="utf-8"), 2)
    body = sections["Gateway probe results"]

    flat_key_names = []
    for key_name, value in record["key"].items():
        if isinstance(value, dict):
            flat_key_names.extend("%s.%s" % (key_name, nested) for nested in value)
        else:
            flat_key_names.append(key_name)
    for name in flat_key_names:
        assert name in body, name

    for name in probe.CAPABILITY_FIELDS:
        assert name in body, name

    for column in ("status", "source", "value", "detail"):
        assert column in body, column

    for name in record["verdict"]:
        assert name in body, name

    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or cells[0] in ("Key field", "Capability", "Verdict field"):
            continue
        if set(cells[0]) <= {"-"}:
            continue
        for value_cell in cells[1:]:
            assert value_cell == "", (line, cells)


def test_plugin_need_is_empty():
    sections = _sections(DECISIONS_PATH.read_text(encoding="utf-8"), 2)
    assert "Plugin need" in sections
    body = sections["Plugin need"].strip()
    assert "intentionally empty" in body.lower()
    for line in body.splitlines():
        line = line.strip()
        assert not line.startswith(("-", "*", "|")), line


_FORBIDDEN_PATTERNS = (
    re.compile(r"\bIVG-\d+\b"),
    re.compile(r"\b(?:AC|FR)-\d+\b"),
    re.compile(r"\bRun \d+\b"),
    re.compile(r"\.workflow_artifacts"),
    re.compile(r"\bM[0-9][ab]?\b"),
    re.compile(r"§\s?\d+"),
)


def _clean_content_corpus():
    texts = {}
    for rel in _shipped_files():
        if rel.endswith((".py", ".json", ".md")):
            texts[rel] = (REPO_ROOT / rel).read_text(encoding="utf-8")
    texts["<status page OpenCode section>"] = _opencode_status_section()
    texts["<adapters README OpenCode bullet>"] = _opencode_adapters_bullet()
    return texts


def test_clean_content_over_shipped_tree():
    for name, text in _clean_content_corpus().items():
        for pattern in _FORBIDDEN_PATTERNS:
            match = pattern.search(text)
            assert match is None, "%s matched %r in %s" % (pattern.pattern, match, name)


def _model_id_denylist():
    raw = (
        "gpt" + "-4o",
        r"\bgpt-[0-9]",
        r"o[1-9]-(mini" + r"|preview)",
        r"claude-(3|opus|sonnet|" + r"haiku)",
        "qwen" + r"[0-9]",
        "gemini-" + r"[0-9]",
        r"llama-?" + r"[0-9]",
        r"deepseek-(v|r|" + r"coder)",
        r"mistral-(large|small|" + r"medium)",
        r"kimi-k" + r"[0-9]",
        r"glm-" + r"[0-9]",
    )
    return [re.compile(p, re.IGNORECASE) for p in raw]


def test_no_real_model_ids():
    patterns = _model_id_denylist()
    for name, text in _clean_content_corpus().items():
        for pattern in patterns:
            match = pattern.search(text)
            assert match is None, "%s matched %r in %s" % (pattern.pattern, match, name)


_SCHEME_HOST_RE = re.compile(r"[a-z][a-z0-9+.-]*://([^/\s\"'`)>\]]+)", re.IGNORECASE)
_BARE_HOST_RE = re.compile(
    r"\b([a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+\."
    r"(?:com|org|net|io|ai|dev|invalid|internal|corp|local|cloud))\b",
    re.IGNORECASE,
)


def _extract_hosts(text):
    hosts = set()
    for m in _SCHEME_HOST_RE.finditer(text):
        hosts.add(m.group(1).split(":")[0].lower())
    for m in _BARE_HOST_RE.finditer(text):
        hosts.add(m.group(1).lower())
    return hosts


def _host_ok(host):
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWLIST)


def test_hostnames_on_allowlist():
    for name, text in _clean_content_corpus().items():
        for host in _extract_hosts(text):
            assert _host_ok(host), "%s in %s not on allowlist" % (host, name)
