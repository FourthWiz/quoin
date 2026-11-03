"""Citation rows the profile-isolation work relies on.

Reads the compatibility document and the anchor manifest only; no network
access. Each verified row carries a machine-readable anchor entry (path, line
range, literal) so a reviewer with a checkout of the pinned tag can re-check
every citation mechanically (set QUOIN_OPENCODE_SOURCE to run that part).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from quoin.opencode_adapter import profile_policy

COMPAT_PATH = helpers.OPENCODE_DIR / "compatibility.md"
ADAPTER_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
MANIFEST_PATH = Path(__file__).resolve().parent / "fixtures" / "opencode_citation_anchors.json"
SECTION = "Headless run events and process lifecycle"
UNVERIFIED_RE = re.compile(r"^unverified\s*(?:—|\s-\s)\s*\S")
REQUIRED_KEYS = (
    "global-xdg-paths",
    "config-wellknown-remote",
    "config-account-remote",
    "config-directories",
    "config-global-files",
    "config-legacy-toml",
    "config-content-env",
    "opencode-permission-env",
    "config-merge-key-order",
    "permission-last-match",
    "agent-markdown-merge-order",
    "agent-permission-merge",
    "command-model-precedence",
    "custom-tool-dirs",
    "plugin-npm-install",
    "npm-install-skip-conditions",
    "npm-config-env",
    "npm-config-precedence",
    "plugin-pure-flag",
    "plugin-default-flag",
    "lsp-default-off",
    "lsp-builtin-ids",
    "lsp-custom-command",
    "formatter-default-off",
    "formatter-builtin-ids",
    "formatter-ruff-uv-pair",
    "lsp-download-flag",
    "models-fetch-flag",
    "runtime-flag-names",
    "flag-truthy-values",
    "flag-config-boolean-forms",
    "claude-code-compat-flags",
    "external-skills-flag",
    "share-flag",
    "database-files",
    "auth-file-shape",
    "config-top-level-keys",
    "markdown-frontmatter-keys",
    "autoupdate-flag",
    "auth-content-env",
    "websearch-gating",
    "webfetch-permission",
    "mcp-server-shapes",
    "remote-instructions",
    "tmp-dir",
    "crash-files",
    "database-backups",
    "provider-fetch-proxy",
    "data-dir-state",
)


def _section_text():
    text = COMPAT_PATH.read_text(encoding="utf-8")
    match = re.search(r"^## " + re.escape(SECTION) + r"\s*$", text, re.MULTILINE)
    assert match, SECTION
    rest = text[match.end():]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def _by_key():
    out = {}
    for line in _section_text().splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == "Claim" or set(cells[0]) <= {"-"}:
            continue
        assert len(cells) == 4, cells
        match = re.match(r"^`key: ([a-z0-9-]+)`", cells[3])
        assert match, cells[3]
        out.setdefault(match.group(1), []).append(cells)
    return out


def _pinned_version():
    text = COMPAT_PATH.read_text(encoding="utf-8")
    match = re.search(r"^Evidence release:\s*(\S+)", text, re.MULTILINE)
    assert match
    return match.group(1)


def _manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def status_of(key):
    return _by_key()[key][0][1]


def is_verified(key):
    return status_of(key) == "verified"


def test_all_isolation_keys_present_once():
    keys = _by_key()
    for key in REQUIRED_KEYS:
        assert key in keys, key
    for key, cells in keys.items():
        assert len(cells) == 1, "duplicate key %s" % key


def test_rows_cite_pinned_lines_or_state_why_not():
    keys = _by_key()
    for key in REQUIRED_KEYS:
        _claim, status, evidence, _note = keys[key][0]
        if status == "verified":
            assert "blob/v1.18.32/" in evidence, key
            assert re.search(r"L\d+", evidence), key
        else:
            assert UNVERIFIED_RE.match(status), key


def _range_text(start, end):
    if start == end:
        return r"(?<![\w-])L%d(?![\d-])" % start
    return r"(?<![\w-])L%d-%d(?!\d)" % (start, end)


def test_manifest_is_consistent_with_the_rows():
    manifest = _manifest()
    assert manifest["version"] == 1
    assert manifest["tag"] == "v" + _pinned_version()
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["commit"])
    keys = _by_key()
    for key in REQUIRED_KEYS:
        if is_verified(key):
            assert manifest["rows"].get(key), "verified key %s has no anchor" % key
        else:
            assert key not in manifest["rows"], "unverified key %s has an anchor" % key
    for key, entries in manifest["rows"].items():
        assert key in keys, key
        evidence = keys[key][0][2]
        for entry in entries:
            start, end = entry["lines"]
            assert 1 <= start <= end, (key, entry)
            assert entry["anchor"], (key, entry)
            assert entry["path"] in evidence, (key, entry["path"])
            assert re.search(_range_text(start, end), evidence), (key, entry)


def test_provider_fetch_proxy_stays_unverified():
    # The row records that the provider path shows no proxy handling in source;
    # if a later read verifies it, this assertion flips together with the row.
    assert not is_verified("provider-fetch-proxy")


def test_unverified_keys_are_not_quoted_in_adapter_code():
    unverified = [key for key in REQUIRED_KEYS if not is_verified(key)]
    assert unverified
    for path in sorted(ADAPTER_DIR.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for key in unverified:
                assert ('"%s"' % key) not in line and ("'%s'" % key) not in line, (path.name, number, key)


def _pinned_count(key):
    match = re.search(r"count of (\d+) is pinned", _by_key()[key][0][3])
    assert match, key
    return int(match.group(1))


def test_code_constants_match_row_status():
    assert profile_policy.LSP_ALLOWLIST_VERIFIED is is_verified("lsp-builtin-ids")
    assert profile_policy.FORMATTER_ALLOWLIST_VERIFIED is is_verified("formatter-builtin-ids")
    pair_verified = is_verified("formatter-ruff-uv-pair")
    expected_pairs = (("ruff", "uv"),) if pair_verified else ()
    assert profile_policy.FORMATTER_PAIRED_IDS == expected_pairs
    if is_verified("lsp-builtin-ids"):
        assert len(set(profile_policy.LSP_BUILTIN_IDS)) == _pinned_count("lsp-builtin-ids")
        assert len(profile_policy.LSP_BUILTIN_IDS) == len(set(profile_policy.LSP_BUILTIN_IDS))
    else:
        assert profile_policy.LSP_BUILTIN_IDS == ()
    if is_verified("formatter-builtin-ids"):
        assert len(set(profile_policy.FORMATTER_BUILTIN_IDS)) == _pinned_count("formatter-builtin-ids")
    else:
        assert profile_policy.FORMATTER_BUILTIN_IDS == ()


@pytest.mark.skipif(
    not os.environ.get("QUOIN_OPENCODE_SOURCE"),
    reason="set QUOIN_OPENCODE_SOURCE to a v1.18.32 checkout to verify citations against source",
)
def test_citations_match_the_source_checkout():
    root = Path(os.environ["QUOIN_OPENCODE_SOURCE"])
    manifest = _manifest()
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head == manifest["commit"]
    for key, entries in manifest["rows"].items():
        for entry in entries:
            target = root / entry["path"]
            assert target.is_file(), (key, entry["path"])
            lines = target.read_text(encoding="utf-8").split("\n")
            start, end = entry["lines"]
            assert end <= len(lines), (key, entry)
            assert entry["anchor"] in "\n".join(lines[start - 1:end]), (key, entry)
