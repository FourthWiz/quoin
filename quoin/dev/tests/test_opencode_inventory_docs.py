"""The README's inventory table is the code's verdict table, row for row."""
from __future__ import annotations

import re
from types import SimpleNamespace

import _opencode_helpers as helpers
from quoin.opencode_adapter import inventory, profile_policy

README = helpers.OPENCODE_DIR / "README.md"
HEADING = "### Configuration inventory"

# code -> (kind, layer, name, facts) for an entry the code is raised for
REPRESENTATIVE = {
    "inventory-plugin": ("plugin", "project", "p", {}),
    "inventory-provider": ("provider", "project", "p", {"in_compiled_set": False}),
    "inventory-disabled-providers": ("disabled-providers", "project", "d", {}),
    "inventory-mcp-local": ("mcp-local", "project", "s", {}),
    "inventory-mcp-remote": ("mcp-remote", "project", "s", {}),
    "inventory-tool": ("tool", "project", "t", {}),
    "inventory-lsp": ("lsp", "project", "l", {}),
    "inventory-formatter": ("formatter", "project", "f", {}),
    "inventory-agent-permission": (
        "agent", "project", "a", {"model": None, "has_permission": True, "has_tools": False, "unknown": False}),
    "inventory-agent-unreadable": (
        "command", "project", "c", {"model": None, "has_permission": False, "has_tools": False, "unknown": True}),
    "inventory-config-permission": ("permission", "global", "permission", {}),
    "inventory-remote-config": ("remote-config", "global", "auth", {"type": "wellknown"}),
    "inventory-account-busy": ("account-config", "account", "db", {"busy": True}),
    "inventory-remote-instructions": ("instruction", "project", "i", {"url": True}),
    "inventory-unknown-key": ("unknown-key", "project", "k", {}),
    "inventory-legacy-config": ("legacy-config", "global", "config", {}),
    "agent-model-outside-allowlist": ("agent", "project", "a", {"model": "x/y"}),
    "command-model-outside-allowlist": ("command", "project", "c", {"model": "x/y"}),
    "config-env-opencode-config": ("config-env", "env", "OPENCODE_CONFIG", {}),
    "config-env-opencode-config-dir": ("config-env", "env", "OPENCODE_CONFIG_DIR", {}),
    "config-env-opencode-config-content": ("config-env", "env", "OPENCODE_CONFIG_CONTENT", {}),
    "executable-fetch-possible": ("fetch-watch", "project", "dir", {"writable": True, "locked": False}),
}
DETECTION_ONLY = {"executable-fetch-detected": ("run fails", "run fails when package installs are denied")}


def _expected(classification):
    policy = profile_policy.view(SimpleNamespace(values={}, classification=classification))
    return inventory.Expected.from_compiled({"provider": {}}, {}, policy)


def _cell(code, classification):
    kind, layer, name, facts = REPRESENTATIVE[code]
    entry = inventory.Entry(kind, layer, 0, "", name, None, False, facts)
    verdict, got = inventory._judge(entry, _expected(classification))  # noqa: SLF001
    return "refused" if got == code else verdict


def _unreadable_cell(classification):
    source = inventory.Source("config-file", "project", "x", None, "unreadable")
    inv = inventory.Inventory([source], [])
    (verdict,) = inventory.evaluate(inv, _expected(classification))
    return "refused" if verdict.code == "config-layer-unreadable" else verdict.verdict


def inventory_rows():
    rows = []
    for code, text in inventory.REFUSAL_TEXT.items():
        if code in DETECTION_ONLY:
            work, personal = DETECTION_ONLY[code]
        elif code == "config-layer-unreadable":
            work, personal = _unreadable_cell("work"), _unreadable_cell("personal")
        else:
            work, personal = _cell(code, "work"), _cell(code, "personal")
        rows.append((code, work, personal, text))
    return rows


def _readme_rows():
    text = README.read_text(encoding="utf-8")
    start = text.index(HEADING)
    block = re.split(r"\n#{2,3} ", text[start + len(HEADING):], maxsplit=1)[0]
    rows = []
    for line in block.splitlines():
        match = re.match(r"^\| `([a-z0-9-]+)` \| (.+?) \| (.+?) \| (.+) \|$", line)
        if match:
            rows.append(match.groups())
    return rows


def test_the_readme_table_matches_the_verdict_table_row_for_row():
    assert _readme_rows() == inventory_rows()


def test_every_judged_code_has_a_representative_entry_that_raises_it_for_work():
    for code in REPRESENTATIVE:
        if code == "executable-fetch-possible":
            continue
        assert _cell(code, "work") == "refused", code
