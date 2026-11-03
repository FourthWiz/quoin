"""The OpenCode release acceptance page stays honest and its pointers resolve."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "quoin" / "adapters" / "opencode" / "release-acceptance.md"
TESTS_DIR = REPO_ROOT / "quoin" / "dev" / "tests"
SPEC = REPO_ROOT / "quoin" / "benchmarks" / "opencode-qualification.json"
SHAPES = ("Personal pilot", "Read-only work", "Managed work")


def _section(text, heading):
    match = re.search(r"^## %s\n(.*?)(?=^## |\Z)" % re.escape(heading), text, re.S | re.M)
    assert match, heading
    return match.group(1)


def _table(section):
    rows = [[c.strip() for c in line.strip().strip("|").split("|")]
            for line in section.splitlines() if line.strip().startswith("|")]
    header, body = rows[0], rows[2:]
    return [dict(zip(header, r)) for r in body]


def _matrix():
    return {row["ID"]: row for row in _table(_section(DOC.read_text(encoding="utf-8"), "Acceptance matrix"))}


def test_matrix_lists_every_criterion_once_in_order():
    assert list(_matrix()) == ["AC%02d" % n for n in range(1, 16)]


def test_every_verification_pointer_is_an_existing_test_file():
    for ident, row in _matrix().items():
        pointers = re.findall(r"`(test_[a-z0-9_]+\.py)`", row["Verification"])
        live = "Live qualification" in row["Verification"]
        assert pointers or live, ident
        for name in pointers:
            assert (TESTS_DIR / name).is_file(), (ident, name)


def test_status_cells_use_the_documented_vocabulary():
    for ident, row in _matrix().items():
        for shape in SHAPES:
            cell = row[shape]
            assert re.match(r"^(pass|pending(: .+)?|n/a(:|,) .+)$", cell), (ident, shape, cell)


def test_release_shape_wording():
    matrix = _matrix()
    assert matrix["AC09"]["Personal pilot"] == "n/a, writes disabled"
    assert matrix["AC09"]["Read-only work"] == "n/a, writes disabled"
    # Managed work needs these in the real execution environment, not mocked policy.
    for ident in ("AC06", "AC07", "AC13"):
        assert matrix[ident]["Managed work"] in ("pending: real environment", "pass"), ident
        assert "real environment" in matrix[ident]["Verification"], ident
    assert matrix["AC09"]["Managed work"].startswith("pending"), "no trusted approval boundary is chosen yet"


def test_cited_quoin_opencode_subcommands_exist():
    cli = (REPO_ROOT / "src" / "quoin" / "cli.py").read_text(encoding="utf-8")
    cited = set(re.findall(r"quoin opencode ([a-z][a-z-]*)", DOC.read_text(encoding="utf-8")))
    assert cited
    for sub in cited:
        assert re.search(r'add_parser\(\s*"%s"' % re.escape(sub), cli), sub


def test_live_rows_cannot_pass_while_results_are_pending():
    text = DOC.read_text(encoding="utf-8")
    results = {row["Profile"]: row for row in _table(_section(text, "Qualification results"))}
    matrix = _matrix()
    work = next(v for k, v in results.items() if k.startswith("Work"))
    personal = next(v for k, v in results.items() if k.startswith("Personal"))
    if "pending" in (work["Tool loop"], work["Scoped tasks"], work["Invariants"]):
        assert matrix["AC04"]["Read-only work"] != "pass"
        assert matrix["AC04"]["Managed work"] != "pass"
        assert "the work profile is not supported" in _section(text, "Current status")
    if "pending" in (personal["Tool loop"], personal["Scoped tasks"], personal["Invariants"]):
        assert matrix["AC05"]["Personal pilot"] != "pass"
        assert "pending live qualification" in _section(text, "Current status")


def test_thresholds_and_pins_match_their_sources():
    text = DOC.read_text(encoding="utf-8")
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    for t in spec["thresholds"]:
        if "required_runs" in t:
            assert "at least %d of %d" % (t["min_successes"], t["required_runs"]) in text, t["id"]
    plugin = (REPO_ROOT / "src" / "quoin" / "opencode_adapter" / "plugin_artifact.py").read_text(encoding="utf-8")
    plugin_version = re.search(r'^PLUGIN_VERSION = "([^"]+)"', plugin, re.M).group(1)
    rows = {row["Component"]: row for row in _table(_section(text, "Compatibility matrix"))}
    from quoin.opencode_adapter import manifest

    qualified = manifest.read_qualified_versions(REPO_ROOT / "quoin")
    assert rows["OpenCode"]["Qualified version"] == ", ".join(qualified)
    assert rows["OpenCode"]["Source of truth"] == "`qualified-versions.json`"
    assert rows["Quoin lifecycle plugin"]["Qualified version"] == plugin_version
